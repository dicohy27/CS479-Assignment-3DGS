"""
PyTorch implementation of Gaussian Splat Rasterizer.

The implementation is based on torch-splatting: https://github.com/hbb1/torch-splatting
"""

from jaxtyping import Bool, Float, jaxtyped
import torch
from typeguard import typechecked


from .camera import Camera
from .scene import Scene
from .sh import eval_sh


class GSRasterizer(object):
    """
    Gaussian Splat Rasterizer.
    """

    def __init__(self):

        self.sh_degree = 3
        self.white_bkgd = True
        self.tile_size = 25

    def render_scene(self, scene: Scene, camera: Camera):

        # Retrieve Gaussian parameters
        mean_3d = scene.mean_3d
        scales = scene.scales
        rotations = scene.rotations
        shs = scene.shs
        opacities = scene.opacities
        
        # ============================================================================
        # Process camera parameters
        # NOTE: We transpose both camera extrinsic and projection matrices
        # assuming that these transforms are applied to points in row vector format.
        # NOTE: Do NOT modify this block.
        # Retrieve camera pose (extrinsic)
        R = camera.camera_to_world[:3, :3]  # 3 x 3
        T = camera.camera_to_world[:3, 3:4]  # 3 x 1
        R_edit = torch.diag(torch.tensor([1, -1, -1], device=R.device, dtype=R.dtype))
        R = R @ R_edit
        R_inv = R.T
        T_inv = -R_inv @ T
        world_to_camera = torch.eye(4, device=R.device, dtype=R.dtype)
        world_to_camera[:3, :3] = R_inv
        world_to_camera[:3, 3:4] = T_inv
        world_to_camera = world_to_camera.permute(1, 0)

        # Retrieve camera intrinsic
        proj_mat = camera.proj_mat.permute(1, 0)
        world_to_camera = world_to_camera.to(mean_3d.device)
        proj_mat = proj_mat.to(mean_3d.device)
        # ============================================================================

        # Project Gaussian center positions to NDC
        mean_ndc, mean_view, in_mask = self.project_ndc(
            mean_3d, world_to_camera, proj_mat, camera.near,
        )
        mean_ndc = mean_ndc[in_mask]
        mean_view = mean_view[in_mask]
        assert mean_ndc.shape[0] > 0, "No points in the frustum"
        assert mean_view.shape[0] > 0, "No points in the frustum"
        depths = mean_view[:, 2]

        # Compute RGB from spherical harmonics
        color = self.get_rgb_from_sh(mean_3d, shs, camera)

        # Compute 3D covariance matrix
        cov_3d = self.compute_cov_3d(scales, rotations)

        # Project covariance matrices to 2D
        cov_2d = self.compute_cov_2d(
            mean_3d=mean_3d, 
            cov_3d=cov_3d, 
            w2c=world_to_camera,
            f_x=camera.f_x, 
            f_y=camera.f_y,
        )
        
        # Compute pixel space coordinates of the projected Gaussian centers
        mean_coord_x = ((mean_ndc[..., 0] + 1) * camera.image_width - 1.0) * 0.5
        mean_coord_y = ((mean_ndc[..., 1] + 1) * camera.image_height - 1.0) * 0.5
        mean_2d = torch.stack([mean_coord_x, mean_coord_y], dim=-1)
        color = self.render(
            camera=camera, 
            mean_2d=mean_2d,
            cov_2d=cov_2d,
            color=color,
            opacities=opacities, 
            depths=depths,
        )
        color = color.reshape(-1, 3)

        return color

    @torch.no_grad()
    def get_rgb_from_sh(self, mean_3d, shs, camera):
        rays_o = camera.cam_center        
        rays_d = mean_3d - rays_o
        rays_d = rays_d / torch.norm(rays_d, dim=-1, keepdim=True)
        color = eval_sh(self.sh_degree, shs.permute(0, 2, 1), rays_d)
        color = torch.clamp_min(color + 0.5, 0.0)
        return color
    
    @jaxtyped(typechecker=typechecked)
    @torch.no_grad()
    def project_ndc(
        self,
        points: Float[torch.Tensor, "N 3"],
        w2c: Float[torch.Tensor, "4 4"],
        proj_mat: Float[torch.Tensor, "4 4"],
        z_near: float,
    ) -> tuple[
        Float[torch.Tensor, "N 4"],
        Float[torch.Tensor, "N 4"],
        Bool[torch.Tensor, "N"],
    ]:
        """
        Projects points to NDC space.
        
        Args:
        - points: 3D points in object space.
        - w2c: World-to-camera matrix.
        - proj_mat: Projection matrix.
        - z_near: Near plane distance.

        Returns:
        - p_ndc: NDC coordinates.
        - p_view: View space coordinates.
        - in_mask: Mask of points that are in the frustum.
        """
        # ========================================================
        # TODO: Implement the projection to NDC space
        points_h = homogenize(points)
        p_view = points_h @ w2c
        p_proj = p_view @ proj_mat
        p_ndc = p_proj / p_proj[..., 3:4]

        # TODO: Cull points that are close or behind the camera
        in_mask = p_ndc[..., 2] >= z_near
        # ========================================================
        return p_ndc, p_view, in_mask

    @torch.no_grad()
    def compute_cov_3d(self, s, r):
        L = build_scaling_rotation(s, r)
        cov3d = L @ L.transpose(1, 2)
        return cov3d

    @jaxtyped(typechecker=typechecked)
    @torch.no_grad()
    def compute_cov_2d(
        self,
        mean_3d: Float[torch.Tensor, "N 3"],
        cov_3d: Float[torch.Tensor, "N 3 3"],
        w2c: Float[torch.Tensor, "4 4"],
        f_x: Float[torch.Tensor, ""],
        f_y: Float[torch.Tensor, ""],
    ) -> Float[torch.Tensor, "N 2 2"]:
        """
        Projects 3D covariances to 2D image plane.

        Args:
        - mean_3d: Coordinates of center of 3D Gaussians.
        - cov_3d: 3D covariance matrix.
        - w2c: World-to-camera matrix.
        - f_x: Focal length along x-axis.
        - f_y: Focal length along y-axis.

        Returns:
        - cov_2d: 2D covariance matrix.
        """ 
        # ========================================================
        # TODO: Transform 3D mean coordinates to camera space
        mean_3d_h = homogenize(mean_3d)
        mean_view = mean_3d_h @ w2c
        mean_view = mean_view[..., :3] / mean_view[..., 3:4]
        mean_3d = mean_view[..., :3]
        # mean_3d = (mean_3d @ w2c[:3, :3]) + w2c[-1:, :3]
        # ========================================================

        # Transpose the rigid transformation part of the world-to-camera matrix
        J = torch.zeros(mean_3d.shape[0], 3, 3).to(mean_3d)
        W = w2c[:3, :3].T
        # ========================================================
        # TODO: Compute Jacobian of view transform and projection
        J[:, 0, 0] = f_x / mean_3d[:, 2]
        J[:, 1, 1] = f_y / mean_3d[:, 2]
        J[:, 0, 2] = -f_x * mean_3d[:, 0] / (mean_3d[:, 2]**2)
        J[:, 1, 2] = -f_y * mean_3d[:, 1] / (mean_3d[:, 2]**2)
        cov_2d = J @ W @ cov_3d @ W.transpose(0, 1) @ J.transpose(1, 2)
        # ========================================================

        # add low pass filter here according to E.q. 32
        filter = torch.eye(2, 2).to(cov_2d) * 0.3
        return cov_2d[:, :2, :2] + filter[None]

    @jaxtyped(typechecker=typechecked)
    @torch.no_grad()
    def render(
        self,
        camera: Camera,
        mean_2d: Float[torch.Tensor, "N 2"],
        cov_2d: Float[torch.Tensor, "N 2 2"],
        color: Float[torch.Tensor, "N 3"],
        opacities: Float[torch.Tensor, "N 1"],
        depths: Float[torch.Tensor, "N"],
    ) -> Float[torch.Tensor, "H W 3"]:
        radii = get_radius(cov_2d) # 타원 반지름
        rect = get_rect(mean_2d, radii, width=camera.image_width, height=camera.image_height) # 타원 포함하는 직사각형

        pix_coord = torch.stack(
            torch.meshgrid(torch.arange(camera.image_height), torch.arange(camera.image_width), indexing='xy'),
            dim=-1,
        ).to(mean_2d.device) #(camera.image_width, camera.image_height, 2)
        
        render_color = torch.ones(*pix_coord.shape[:2], 3).to(mean_2d.device)

        assert camera.image_height % self.tile_size == 0, "Image height must be divisible by the tile_size."
        assert camera.image_width % self.tile_size == 0, "Image width must be divisible by the tile_size."
        for h in range(0, camera.image_height, self.tile_size):
            for w in range(0, camera.image_width, self.tile_size):
                # check if the rectangle penetrate the tile
                over_tl = rect[0][..., 0].clip(min=w), rect[0][..., 1].clip(min=h)
                over_br = rect[1][..., 0].clip(max=w+self.tile_size-1), rect[1][..., 1].clip(max=h+self.tile_size-1)
                
                # a binary mask indicating projected Gaussians that lie in the current tile
                in_mask = (over_br[0] > over_tl[0]) & (over_br[1] > over_tl[1])
                if not in_mask.sum() > 0:
                    continue

                # ========================================================
                # TODO: Sort the projected Gaussians that lie in the current tile by their depths, in ascending order
                sorted_idx = torch.argsort(depths[in_mask])
                sorted_mean_2d = mean_2d[in_mask][sorted_idx] # [m, 2]
                sorted_cov_2d_inv = cov_2d[in_mask][sorted_idx].inverse() # [m, 2, 2]
                sorted_color = color[in_mask][sorted_idx] # [m, 3]
                sorted_opacities = opacities[in_mask][sorted_idx] # [m, 1]
                # ========================================================
                
                # ========================================================
                # TODO: Compute the displacement vector from the 2D mean coordinates to the pixel coordinates
                disp_vec = pix_coord[h:h+self.tile_size, w:w+self.tile_size].unsqueeze(2) - sorted_mean_2d.unsqueeze(0).unsqueeze(0) # [t, t, m, 2]
                disp_vec = disp_vec.unsqueeze(-1) # [t, t, m, 2, 1]
                # ========================================================

                # ========================================================
                # TODO: Compute the Gaussian weight for each pixel in the tile
                # ========================================================
                gaussian_weight = torch.exp(-1/2 * disp_vec.transpose(3, 4) @ sorted_cov_2d_inv @ disp_vec).squeeze(-1) # [tw, th, m, 1]
                # ========================================================
                # TODO: Perform alpha blending
                weighted_opacity = (sorted_opacities * gaussian_weight) # [t, t, m, 1]
                prods = torch.cat([torch.ones_like(weighted_opacity[:, :, 0].unsqueeze(-1)),
                                   torch.cumprod((1-weighted_opacity)[:, :, :-1], dim=-2)], dim=-2) # [t, t, m, 1]
                acc_prods = (weighted_opacity * prods).sum(dim=-2)
                tile_color = torch.sum(sorted_color * weighted_opacity * prods, dim=-2) + (1 - acc_prods) # [t, t, 3]
                # ========================================================

                render_color[h:h+self.tile_size, w:w+self.tile_size] = tile_color.reshape(self.tile_size, self.tile_size, -1)

        return render_color

@torch.no_grad()
def homogenize(points):
    """
    homogeneous points
    :param points: [..., 3]
    """
    return torch.cat([points, torch.ones_like(points[..., :1])], dim=-1)

@torch.no_grad()
def build_rotation(r):
    norm = torch.sqrt(r[:,0]*r[:,0] + r[:,1]*r[:,1] + r[:,2]*r[:,2] + r[:,3]*r[:,3])

    q = r / norm[:, None]

    R = torch.zeros((q.size(0), 3, 3), device='cuda')

    r = q[:, 0]
    x = q[:, 1]
    y = q[:, 2]
    z = q[:, 3]

    R[:, 0, 0] = 1 - 2 * (y*y + z*z)
    R[:, 0, 1] = 2 * (x*y - r*z)
    R[:, 0, 2] = 2 * (x*z + r*y)
    R[:, 1, 0] = 2 * (x*y + r*z)
    R[:, 1, 1] = 1 - 2 * (x*x + z*z)
    R[:, 1, 2] = 2 * (y*z - r*x)
    R[:, 2, 0] = 2 * (x*z - r*y)
    R[:, 2, 1] = 2 * (y*z + r*x)
    R[:, 2, 2] = 1 - 2 * (x*x + y*y)
    return R

@torch.no_grad()
def build_scaling_rotation(s, r):
    L = torch.zeros((s.shape[0], 3, 3), dtype=torch.float, device="cuda")
    R = build_rotation(r)

    L[:,0,0] = s[:,0]
    L[:,1,1] = s[:,1]
    L[:,2,2] = s[:,2]

    L = R @ L
    return L

@torch.no_grad()
def get_radius(cov2d):
    det = cov2d[:, 0, 0] * cov2d[:, 1, 1] - cov2d[:, 0, 1] * cov2d[:, 1, 0]
    mid = 0.5 * (cov2d[:, 0, 0] + cov2d[:, 1, 1])
    lambda1 = mid + torch.sqrt((mid**2-det).clip(min=0.1))
    lambda2 = mid - torch.sqrt((mid**2-det).clip(min=0.1))
    return 3.0 * torch.sqrt(torch.max(lambda1, lambda2)).ceil()

@torch.no_grad()
def get_rect(pix_coord, radii, width, height):
    rect_min = (pix_coord - radii[:,None])
    rect_max = (pix_coord + radii[:,None])
    rect_min[..., 0] = rect_min[..., 0].clip(0, width - 1.0)
    rect_min[..., 1] = rect_min[..., 1].clip(0, height - 1.0)
    rect_max[..., 0] = rect_max[..., 0].clip(0, width - 1.0)
    rect_max[..., 1] = rect_max[..., 1].clip(0, height - 1.0)
    return rect_min, rect_max
